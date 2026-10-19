from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from autosport.deployment_semantic_scope import (
    DeploymentSemanticAuthority,
    DeploymentSemanticScope,
)
from autosport.learning_environment import EnvironmentIdentity
from autosport.policy_deployment import ActivationBinding, DeploymentScope
from autosport.policy_deployment_semantic_bridge import (
    CrossSessionSemanticInputs,
    PolicyDeploymentSemanticBridgeError,
    SemanticResolutionInput,
    load_semantic_binding,
    validate_canonical_activation_binding,
)


T1 = "2026-09-20T01:00:00Z"
T2 = "2026-09-20T02:00:00Z"
T3 = "2026-09-20T03:00:00Z"
CONFIG_SHA = "c" * 64
GOAL_SHA = "a" * 64
RISK_SHA = "b" * 64
TRAINING_RUNTIME_ID = "3" * 64
DEPLOYMENT_RUNTIME_ID = "4" * 64


def _identity(data_id: str, cutoff: str) -> EnvironmentIdentity:
    return EnvironmentIdentity(
        source_id="lawful-provider:paper",
        config_id="config-v1",
        data_id=data_id,
        protocol_id="protocol-v1",
        cutoff_ts=cutoff,
        seed=7,
    )


def _scope() -> DeploymentSemanticScope:
    return DeploymentSemanticScope(
        sport_domain="football",
        competition_scope="league:test",
        market_semantics_id="match-odds-v1",
        provider_source_class="lawful-provider",
        dataset_source_identity="lawful-provider:paper",
        dataset_license_identity="paper-license-v1",
        feature_set_id="features-v1",
        feature_set_version="1",
        feature_definition_sha256="f" * 64,
        feature_source_sha256="e" * 64,
        research_protocol_semantics_id="d" * 64,
        research_protocol_id="protocol-v1",
        research_protocol_sha256="9" * 64,
        config_id="config-v1",
        config_sha256=CONFIG_SHA,
        action_semantics_id="8" * 64,
        action_semantics_definition_sha256="7" * 64,
        reward_definition_id="paper-settlement-learning-reward-v1",
    )


def _authority(
    identity: EnvironmentIdentity,
    *,
    dataset_sha: str,
    runtime_id: str,
    marker: str,
) -> DeploymentSemanticAuthority:
    return DeploymentSemanticAuthority(
        scope=_scope(),
        decision_ts=T3,
        market_event_dedupe_key=f"event-{marker}",
        market_event_payload_sha256=marker * 64,
        event_quote_key=f"quote-{marker}",
        provider_source_id="lawful-provider:paper",
        event_observed_ts=T1,
        dataset_snapshot_id=identity.data_id,
        dataset_record_sha256=dataset_sha,
        dataset_manifest_sha256="6" * 64,
        dataset_causal_cutoff=identity.cutoff_ts,
        dataset_available_at=identity.cutoff_ts,
        feature_record_sha256="5" * 64,
        feature_available_at=T1,
        protocol_record_sha256="9" * 64,
        protocol_available_at=T1,
        runtime_authority_id=runtime_id,
        runtime_record_sha256=("1" if marker != "2" else "2") * 64,
        runtime_available_at=T1,
        environment_id=identity.environment_id,
        episode_id=("a" if marker != "2" else "b") * 64,
    )


def _legacy_scope() -> DeploymentScope:
    canonical = _scope()
    return DeploymentScope(
        canonical_strategy_id="strategy-v1",
        sport_domain=canonical.sport_domain,
        competition_scope=canonical.competition_scope,
        market_semantics_id=canonical.market_semantics_id,
        provider_source_class=canonical.provider_source_class,
        feature_schema_id=canonical.feature_definition_sha256,
        protocol_id=canonical.research_protocol_id,
        action_semantics_id=canonical.action_semantics_id,
        reward_definition_id=canonical.reward_definition_id,
        config_sha256=canonical.config_sha256,
    )


def _binding(training: EnvironmentIdentity, deployment: EnvironmentIdentity) -> ActivationBinding:
    return ActivationBinding(
        policy_id="0" * 64,
        policy_artifact_sha256="1" * 64,
        training_environment_id=training.environment_id,
        training_data_id=training.data_id,
        training_dataset_record_sha256="1" * 64,
        training_cutoff_ts=training.cutoff_ts,
        promotion_decision_id="promotion-v1",
        promotion_decision_record_sha256="2" * 64,
        promotion_evidence_id="3" * 64,
        promotion_evidence_record_sha256="4" * 64,
        evaluation_bundle_id="evaluation-v1",
        evaluation_bundle_record_sha256="5" * 64,
        deployment_scope_id=_legacy_scope().scope_id,
        deployment_environment_id=deployment.environment_id,
        deployment_data_id=deployment.data_id,
        deployment_dataset_record_sha256="2" * 64,
        dataset_lineage_proof_sha256="6" * 64,
        deployment_cutoff_ts=deployment.cutoff_ts,
        snapshot_available_at=T2,
        activation_at=T3,
        admissible_actions=("WAIT",),
        economic_goal_fingerprint=GOAL_SHA,
        risk_fingerprint=RISK_SHA,
    )


def _inputs() -> CrossSessionSemanticInputs:
    return CrossSessionSemanticInputs(
        training=SemanticResolutionInput(
            market_event_dedupe_key="training-event",
            feature_set_id="features-v1",
            runtime_authority_id=TRAINING_RUNTIME_ID,
        ),
        deployment=SemanticResolutionInput(
            market_event_dedupe_key="deployment-event",
            feature_set_id="features-v1",
            runtime_authority_id=DEPLOYMENT_RUNTIME_ID,
        ),
    )


def _validate(tmp_path, *, resolver_side_effect, require_existing: bool):
    training = _identity("training-dataset", T1)
    deployment = _identity("deployment-dataset", T2)
    binding = _binding(training, deployment)
    loop_path = tmp_path / "agent-loop.json"
    with patch(
        "autosport.policy_deployment_semantic_bridge.resolve_deployment_semantic_scope",
        side_effect=resolver_side_effect,
    ) as resolver, patch(
        "autosport.policy_deployment_semantic_bridge.validate_activation_binding"
    ) as legacy_validator:
        result = validate_canonical_activation_binding(
            binding,
            semantic_inputs=_inputs(),
            market_store=object(),
            runtime_authority_store=object(),
            loop_path=loop_path,
            require_existing_semantic_binding=require_existing,
            policy=object(),
            training_identity=training,
            deployment_identity=deployment,
            registry=object(),
            artifact_store=object(),
            canonical_strategy_id="strategy-v1",
            admissible_actions=frozenset({"WAIT"}),
            economic_goal_fingerprint=GOAL_SHA,
            risk_fingerprint=RISK_SHA,
        )
    assert resolver.call_count == 2
    legacy_validator.assert_called_once()
    return loop_path, binding, result


def test_canonical_semantics_are_resolved_and_pinned_for_restart(tmp_path) -> None:
    training = _identity("training-dataset", T1)
    deployment = _identity("deployment-dataset", T2)
    training_authority = _authority(
        training,
        dataset_sha="1" * 64,
        runtime_id=TRAINING_RUNTIME_ID,
        marker="1",
    )
    deployment_authority = _authority(
        deployment,
        dataset_sha="2" * 64,
        runtime_id=DEPLOYMENT_RUNTIME_ID,
        marker="2",
    )
    loop_path, binding, result = _validate(
        tmp_path,
        resolver_side_effect=(training_authority, deployment_authority),
        require_existing=False,
    )

    durable = load_semantic_binding(
        loop_path,
        expected_activation_binding_id=binding.binding_id,
    )
    assert durable == result.semantic_binding
    assert durable.training_authority_id == training_authority.authority_id
    assert durable.deployment_authority_id == deployment_authority.authority_id

    _validate(
        tmp_path,
        resolver_side_effect=(training_authority, deployment_authority),
        require_existing=True,
    )


def test_restart_rejects_same_scope_with_changed_exact_authority(tmp_path) -> None:
    training = _identity("training-dataset", T1)
    deployment = _identity("deployment-dataset", T2)
    training_authority = _authority(
        training,
        dataset_sha="1" * 64,
        runtime_id=TRAINING_RUNTIME_ID,
        marker="1",
    )
    deployment_authority = _authority(
        deployment,
        dataset_sha="2" * 64,
        runtime_id=DEPLOYMENT_RUNTIME_ID,
        marker="2",
    )
    _validate(
        tmp_path,
        resolver_side_effect=(training_authority, deployment_authority),
        require_existing=False,
    )
    changed = replace(deployment_authority, runtime_record_sha256="e" * 64)
    with pytest.raises(
        PolicyDeploymentSemanticBridgeError,
        match="changed across restart",
    ):
        _validate(
            tmp_path,
            resolver_side_effect=(training_authority, changed),
            require_existing=True,
        )


def test_self_consistent_caller_scope_cannot_replace_canonical_scope(tmp_path) -> None:
    training = _identity("training-dataset", T1)
    deployment = _identity("deployment-dataset", T2)
    training_authority = _authority(
        training,
        dataset_sha="1" * 64,
        runtime_id=TRAINING_RUNTIME_ID,
        marker="1",
    )
    incompatible_scope = replace(_scope(), competition_scope="league:other")
    deployment_authority = replace(
        _authority(
            deployment,
            dataset_sha="2" * 64,
            runtime_id=DEPLOYMENT_RUNTIME_ID,
            marker="2",
        ),
        scope=incompatible_scope,
    )
    with pytest.raises(
        PolicyDeploymentSemanticBridgeError,
        match="canonical semantic scopes are incompatible",
    ):
        _validate(
            tmp_path,
            resolver_side_effect=(training_authority, deployment_authority),
            require_existing=False,
        )


def test_cross_sport_activation_fails_even_when_other_semantics_match(tmp_path) -> None:
    training = _identity("training-dataset", T1)
    deployment = _identity("deployment-dataset", T2)
    training_scope = _scope()
    second_sport_scope = replace(
        training_scope,
        sport_domain="test-only-second-sport",
    )

    # Sport is an authority-bearing compatibility dimension, not presentation
    # metadata. Keep every other semantic dimension byte-for-byte identical.
    assert training_scope.scope_id != second_sport_scope.scope_id
    assert (
        replace(second_sport_scope, sport_domain=training_scope.sport_domain)
        == training_scope
    )

    training_authority = _authority(
        training,
        dataset_sha="1" * 64,
        runtime_id=TRAINING_RUNTIME_ID,
        marker="1",
    )
    deployment_authority = replace(
        _authority(
            deployment,
            dataset_sha="2" * 64,
            runtime_id=DEPLOYMENT_RUNTIME_ID,
            marker="2",
        ),
        scope=second_sport_scope,
    )

    with pytest.raises(
        PolicyDeploymentSemanticBridgeError,
        match="canonical semantic scopes are incompatible",
    ):
        _validate(
            tmp_path,
            resolver_side_effect=(training_authority, deployment_authority),
            require_existing=False,
        )


def test_missing_semantic_binding_fails_closed_on_resume(tmp_path) -> None:
    training = _identity("training-dataset", T1)
    deployment = _identity("deployment-dataset", T2)
    with pytest.raises(
        PolicyDeploymentSemanticBridgeError,
        match="binding is missing",
    ):
        _validate(
            tmp_path,
            resolver_side_effect=(
                _authority(
                    training,
                    dataset_sha="1" * 64,
                    runtime_id=TRAINING_RUNTIME_ID,
                    marker="1",
                ),
                _authority(
                    deployment,
                    dataset_sha="2" * 64,
                    runtime_id=DEPLOYMENT_RUNTIME_ID,
                    marker="2",
                ),
            ),
            require_existing=True,
        )
