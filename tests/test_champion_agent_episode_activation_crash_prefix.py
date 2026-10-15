from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.champion_agent_episode import ChampionAgentEpisode
from autosport.policy_deployment import (
    deployment_authority_path,
    load_deployment_authority,
)
from autosport.policy_deployment_semantic_bridge import (
    CanonicalSemanticBinding,
    load_semantic_binding,
    persist_semantic_binding,
    semantic_binding_path,
)
from test_champion_agent_episode_cross_session_boundary import (
    CONFIG_SHA,
    GOAL_SHA,
    RISK_SHA,
    SOURCE_SHA,
    STRATEGY_ID,
    T1,
    T2,
    _binding,
    _identity,
    _policy,
    _scope,
)


def _semantic_binding(activation_binding, scope):
    return CanonicalSemanticBinding(
        activation_binding_id=activation_binding.binding_id,
        compatibility_scope_id=scope.scope_id,
        training_authority_id="1" * 64,
        deployment_authority_id="2" * 64,
        training_market_event_dedupe_key="training-event",
        deployment_market_event_dedupe_key="deployment-event",
        training_runtime_authority_id="3" * 64,
        deployment_runtime_authority_id="4" * 64,
    )


def _activation_kwargs(tmp_path):
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    policy = _policy(training)
    scope = _scope()
    binding = _binding(policy, training, deployment, scope)
    path = tmp_path / "agent-loop.json"
    semantic_inputs = object()
    market_store = object()
    runtime_store = object()
    kwargs = dict(
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
    return path, training, deployment, policy, scope, binding, kwargs


def _semantic_validator(path, binding, scope):
    durable = _semantic_binding(binding, scope)

    def validate(*args, **kwargs):
        assert kwargs["require_existing_semantic_binding"] is False
        persist_semantic_binding(path, durable)
        return SimpleNamespace(deployment_scope=scope)

    return durable, validate


def _resolver_boundary(policy, validate):
    return (
        patch(
            "autosport.champion_agent_episode._require_canonical_inputs",
            side_effect=lambda **kwargs: (
                kwargs["semantic_inputs"],
                kwargs["market_store"],
                kwargs["runtime_authority_store"],
            ),
        ),
        patch(
            "autosport.champion_agent_episode.load_champion_policy",
            return_value=policy,
        ),
        patch(
            "autosport.champion_agent_episode.validate_canonical_activation_binding",
            side_effect=validate,
        ),
    )


def _initialize(path, kwargs):
    return ChampionAgentEpisode.initialize_pristine(
        path,
        object(),
        object(),
        **kwargs,
    )


def test_retry_converges_after_crash_between_semantic_and_deployment_authority(
    tmp_path,
) -> None:
    path, training, deployment, policy, scope, binding, kwargs = _activation_kwargs(
        tmp_path
    )
    expected_semantic, validate = _semantic_validator(path, binding, scope)
    require_inputs, load_policy, validate_patch = _resolver_boundary(policy, validate)

    with require_inputs, load_policy, validate_patch, patch(
        "autosport.champion_agent_episode.persist_deployment_authority",
        side_effect=OSError("crash before deployment authority publication"),
    ):
        with pytest.raises(
            OSError, match="crash before deployment authority publication"
        ):
            _initialize(path, kwargs)

    assert semantic_binding_path(path).exists()
    assert load_semantic_binding(path) == expected_semantic
    assert not deployment_authority_path(path).exists()
    assert not path.exists()

    require_inputs, load_policy, validate_patch = _resolver_boundary(policy, validate)
    with require_inputs, load_policy, validate_patch:
        session = _initialize(path, kwargs)

    assert session.agent_loop.snapshot().activation_binding_id == binding.binding_id
    assert load_semantic_binding(path) == expected_semantic
    durable = load_deployment_authority(
        path,
        expected_binding_id=binding.binding_id,
    )
    assert durable.training_identity == training
    assert durable.deployment_identity == deployment
    assert durable.scope == scope
    assert durable.binding == binding


def test_retry_converges_after_crash_between_deployment_authority_and_agent_loop(
    tmp_path,
) -> None:
    path, training, deployment, policy, scope, binding, kwargs = _activation_kwargs(
        tmp_path
    )
    expected_semantic, validate = _semantic_validator(path, binding, scope)
    require_inputs, load_policy, validate_patch = _resolver_boundary(policy, validate)

    with require_inputs, load_policy, validate_patch, patch(
        "autosport.champion_agent_episode.AgentLoopRuntime.initialize_pristine",
        side_effect=OSError("crash before AgentLoop publication"),
    ):
        with pytest.raises(OSError, match="crash before AgentLoop publication"):
            _initialize(path, kwargs)

    assert semantic_binding_path(path).exists()
    assert load_semantic_binding(path) == expected_semantic
    durable_before_retry = load_deployment_authority(
        path,
        expected_binding_id=binding.binding_id,
    )
    assert durable_before_retry.training_identity == training
    assert durable_before_retry.deployment_identity == deployment
    assert durable_before_retry.scope == scope
    assert durable_before_retry.binding == binding
    assert not path.exists()

    require_inputs, load_policy, validate_patch = _resolver_boundary(policy, validate)
    with require_inputs, load_policy, validate_patch:
        session = _initialize(path, kwargs)

    assert session.agent_loop.snapshot().activation_binding_id == binding.binding_id
    assert load_semantic_binding(path) == expected_semantic
    assert (
        load_deployment_authority(path, expected_binding_id=binding.binding_id)
        == durable_before_retry
    )
