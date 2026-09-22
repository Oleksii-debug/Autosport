from unittest.mock import patch

import pytest

from autosport.policy_deployment import PolicyDeploymentError, validate_activation_binding
from autosport.scientific_registry import ScientificRegistry
from tests.test_policy_deployment_champion_eligibility import (
    ACTIVATION_AT,
    GOAL_SHA,
    MODEL_ID,
    RISK_SHA,
    STRATEGY_KEY,
    _record_resolver,
    _setup,
)


CURRENT_AS_OF = "2026-09-20T05:00:00Z"


def test_restart_revalidates_champion_and_eligibility_at_current_as_of(tmp_path) -> None:
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
        side_effect=(policy.policy_id, policy.policy_id),
    ) as champion_strategy, patch(
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
            current_eligibility_as_of=CURRENT_AS_OF,
        )

    assert [call.kwargs["as_of"] for call in champion_strategy.call_args_list] == [
        ACTIVATION_AT,
        CURRENT_AS_OF,
    ]
    assert current_eligibility.call_args.kwargs["as_of"] == CURRENT_AS_OF
    assert current_eligibility.call_args.kwargs["expected_model_version_id"] == MODEL_ID


def test_restart_cannot_keep_cached_policy_after_current_champion_changes(tmp_path) -> None:
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
        side_effect=(policy.policy_id, "replacement-strategy"),
    ), patch(
        "autosport.policy_deployment.require_current_activation_eligibility"
    ) as current_eligibility:
        with pytest.raises(
            PolicyDeploymentError,
            match="no longer the durable champion",
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
                current_eligibility_as_of=CURRENT_AS_OF,
            )

    current_eligibility.assert_not_called()


def test_current_eligibility_time_cannot_precede_original_activation(tmp_path) -> None:
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
    ):
        with pytest.raises(
            PolicyDeploymentError,
            match="current eligibility time precedes deployment activation",
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
                current_eligibility_as_of="2026-09-20T03:30:00Z",
            )
