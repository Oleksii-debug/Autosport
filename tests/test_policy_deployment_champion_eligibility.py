from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.champion_eligibility import ChampionEligibilityError
from autosport.champion_policy import POLICY_ARTIFACT_KIND, persist_policy_state
from autosport.learning_environment import EnvironmentIdentity
from autosport.policy_deployment import (
    ActivationBinding,
    DeploymentScope,
    PolicyDeploymentError,
    validate_activation_binding,
)
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore
from autosport.transparent_bandit_policy import BanditPolicyState


CONFIG_SHA = "c" * 64
GOAL_SHA = "a" * 64
RISK_SHA = "b" * 64
MODEL_ID = "model-current-v1"
PROTOCOL_ID = "protocol-current-eligibility-v1"
STRATEGY_KEY = "canonical-transparent-bandit"
TRAINING_CUTOFF = "2026-09-20T01:00:00Z"
DEPLOYMENT_CUTOFF = "2026-09-20T02:00:00Z"
PROMOTION_AT = "2026-09-20T02:30:00Z"
SNAPSHOT_AT = "2026-09-20T03:00:00Z"
ACTIVATION_AT = "2026-09-20T04:00:00Z"


def _identity(data_id: str, cutoff: str) -> EnvironmentIdentity:
    return EnvironmentIdentity(
        source_id="lawful-provider:paper",
        config_id="config-current-eligibility-v1",
        data_id=data_id,
        protocol_id=PROTOCOL_ID,
        cutoff_ts=cutoff,
        seed=7,
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
        canonical_strategy_id=STRATEGY_KEY,
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
    artifact_store: FactoryArtifactStore,
) -> ActivationBinding:
    return ActivationBinding(
        policy_id=policy.policy_id,
        policy_artifact_sha256=artifact_store.sha256(
            POLICY_ARTIFACT_KIND, policy.policy_id
        ),
        training_environment_id=training.environment_id,
        training_data_id=training.data_id,
        training_dataset_record_sha256="1" * 64,
        training_cutoff_ts=training.cutoff_ts,
        promotion_decision_id="promotion-current-eligibility-v1",
        promotion_decision_record_sha256="2" * 64,
        promotion_evidence_id="3" * 64,
        promotion_evidence_record_sha256="4" * 64,
        evaluation_bundle_id="evaluation-current-eligibility-v1",
        evaluation_bundle_record_sha256="5" * 64,
        deployment_scope_id=scope.scope_id,
        deployment_environment_id=deployment.environment_id,
        deployment_data_id=deployment.data_id,
        deployment_dataset_record_sha256="6" * 64,
        dataset_lineage_proof_sha256="7" * 64,
        deployment_cutoff_ts=deployment.cutoff_ts,
        snapshot_available_at=SNAPSHOT_AT,
        activation_at=ACTIVATION_AT,
        admissible_actions=("WAIT",),
        economic_goal_fingerprint=GOAL_SHA,
        risk_fingerprint=RISK_SHA,
    )


def _record_resolver(
    policy: BanditPolicyState,
    training: EnvironmentIdentity,
    deployment: EnvironmentIdentity,
    *,
    candidate_model_version_id: str | None = MODEL_ID,
):
    records = {
        "DatasetSnapshot": (
            SimpleNamespace(
                record_type="DatasetSnapshot",
                record_id=training.data_id,
                available_at=TRAINING_CUTOFF,
                payload={
                    "dataset_snapshot_id": training.data_id,
                    "causal_cutoff": training.cutoff_ts,
                    "source_identity": "lawful-provider:paper",
                    "license_identity": "paper-license-v1",
                },
            ),
            SimpleNamespace(
                record_type="DatasetSnapshot",
                record_id=deployment.data_id,
                available_at=SNAPSHOT_AT,
                payload={
                    "dataset_snapshot_id": deployment.data_id,
                    "causal_cutoff": deployment.cutoff_ts,
                    "source_identity": "lawful-provider:paper",
                    "license_identity": "paper-license-v1",
                },
            ),
        ),
        "PromotionDecision": (
            SimpleNamespace(
                record_type="PromotionDecision",
                record_id="promotion-current-eligibility-v1",
                available_at=PROMOTION_AT,
                payload={
                    "action": "PROMOTE",
                    "candidate_strategy_version_id": policy.policy_id,
                    "candidate_model_version_id": candidate_model_version_id,
                    "promotion_evidence_id": "3" * 64,
                    "evaluation_bundle_id": "evaluation-current-eligibility-v1",
                    "evaluation_bundle_sha256": "9" * 64,
                    "research_protocol_id": PROTOCOL_ID,
                },
            ),
        ),
        "PromotionEvidence": (
            SimpleNamespace(
                record_type="PromotionEvidence",
                record_id="3" * 64,
                available_at=PROMOTION_AT,
                payload={
                    "candidate_strategy_version_id": policy.policy_id,
                    "candidate_model_version_id": candidate_model_version_id,
                    "evaluation_bundle_id": "evaluation-current-eligibility-v1",
                    "evaluation_bundle_sha256": "9" * 64,
                    "research_protocol_id": PROTOCOL_ID,
                    "dataset_snapshot_id": training.data_id,
                    "validity": "ELIGIBLE",
                },
            ),
        ),
        "EvaluationBundle": (
            SimpleNamespace(
                record_type="EvaluationBundle",
                record_id="evaluation-current-eligibility-v1",
                available_at=PROMOTION_AT,
                payload={
                    "evaluation_bundle_id": "evaluation-current-eligibility-v1",
                    "evaluated_strategy_version_id": policy.policy_id,
                    "dataset_snapshot_id": training.data_id,
                    "bundle_sha256": "9" * 64,
                },
            ),
        ),
    }

    def resolve(
        _registry,
        *,
        record_type,
        record_id,
        record_sha256,
        as_of,
    ):
        del _registry, record_sha256, as_of
        matches = [record for record in records[record_type] if record.record_id == record_id]
        assert len(matches) == 1
        return matches[0]

    return resolve


def _setup(tmp_path):
    training = _identity("training-current-eligibility", TRAINING_CUTOFF)
    deployment = _identity("deployment-current-eligibility", DEPLOYMENT_CUTOFF)
    policy = _policy(training)
    registry = ScientificRegistry.initialize_pristine(tmp_path / "registry.json")
    artifact_store = FactoryArtifactStore(tmp_path / "artifacts")
    persist_policy_state(artifact_store, policy)
    scope = _scope()
    binding = _binding(policy, training, deployment, scope, artifact_store)
    return training, deployment, policy, registry, artifact_store, scope, binding


def test_model_backed_cross_session_deployment_requires_current_eligibility(tmp_path) -> None:
    (
        training,
        deployment,
        policy,
        registry,
        artifact_store,
        scope,
        binding,
    ) = _setup(tmp_path)

    with patch(
        "autosport.policy_deployment._causal_record",
        side_effect=_record_resolver(policy, training, deployment),
    ), patch.object(
        ScientificRegistry,
        "champion_strategy",
        autospec=True,
        return_value=policy.policy_id,
    ), patch(
        "autosport.policy_deployment.require_current_activation_eligibility"
    ) as current_eligibility:
        validate_activation_binding(
            binding,
            scope=scope,
            policy=policy,
            training_identity=training,
            deployment_identity=deployment,
            registry=registry,
            artifact_store=artifact_store,
            canonical_strategy_id=STRATEGY_KEY,
            admissible_actions=frozenset({"WAIT"}),
            economic_goal_fingerprint=GOAL_SHA,
            risk_fingerprint=RISK_SHA,
        )

    current_eligibility.assert_called_once()
    kwargs = current_eligibility.call_args.kwargs
    assert kwargs["as_of"] == ACTIVATION_AT
    assert kwargs["expected_strategy_version_id"] == policy.policy_id
    assert kwargs["expected_model_version_id"] == MODEL_ID
    assert kwargs["expected_environment_sha256"] == deployment.environment_id
    assert kwargs["expected_sport"] == "football"
    assert kwargs["expected_league"] == "league:test"
    assert kwargs["expected_regime"] is None
    assert kwargs["admissible_actions"] == frozenset({"WAIT"})


def test_expired_current_eligibility_blocks_old_promotion_history(tmp_path) -> None:
    (
        training,
        deployment,
        policy,
        registry,
        artifact_store,
        scope,
        binding,
    ) = _setup(tmp_path)

    with patch(
        "autosport.policy_deployment._causal_record",
        side_effect=_record_resolver(policy, training, deployment),
    ), patch.object(
        ScientificRegistry,
        "champion_strategy",
        autospec=True,
        return_value=policy.policy_id,
    ), patch(
        "autosport.policy_deployment.require_current_activation_eligibility",
        side_effect=ChampionEligibilityError("eligibility decision is expired"),
    ):
        with pytest.raises(
            PolicyDeploymentError,
            match="current champion eligibility does not authorize deployment",
        ):
            validate_activation_binding(
                binding,
                scope=scope,
                policy=policy,
                training_identity=training,
                deployment_identity=deployment,
                registry=registry,
                artifact_store=artifact_store,
                canonical_strategy_id=STRATEGY_KEY,
                admissible_actions=frozenset({"WAIT"}),
                economic_goal_fingerprint=GOAL_SHA,
                risk_fingerprint=RISK_SHA,
            )


def test_legacy_strategy_only_promotion_does_not_invent_model_identity(tmp_path) -> None:
    (
        training,
        deployment,
        policy,
        registry,
        artifact_store,
        scope,
        binding,
    ) = _setup(tmp_path)

    with patch(
        "autosport.policy_deployment._causal_record",
        side_effect=_record_resolver(
            policy,
            training,
            deployment,
            candidate_model_version_id=None,
        ),
    ), patch.object(
        ScientificRegistry,
        "champion_strategy",
        autospec=True,
        return_value=policy.policy_id,
    ), patch(
        "autosport.policy_deployment.require_current_activation_eligibility"
    ) as current_eligibility:
        validate_activation_binding(
            binding,
            scope=scope,
            policy=policy,
            training_identity=training,
            deployment_identity=deployment,
            registry=registry,
            artifact_store=artifact_store,
            canonical_strategy_id=STRATEGY_KEY,
            admissible_actions=frozenset({"WAIT"}),
            economic_goal_fingerprint=GOAL_SHA,
            risk_fingerprint=RISK_SHA,
        )

    current_eligibility.assert_not_called()
