from autosport.champion_agent_episode import ChampionAgentEpisode
from tests.test_champion_agent_episode_cross_session_boundary import (
    CONFIG_SHA,
    GOAL_SHA,
    RISK_SHA,
    STRATEGY_ID,
    T1,
    T2,
    _binding,
    _identity,
    _policy,
    _resolver_patches,
    _scope,
)


CURRENT_AT_START = "2026-09-20T03:00:00Z"
CURRENT_AT_RESTART = "2026-09-20T04:00:00Z"


def _initialize(tmp_path):
    training = _identity("training-current-eligibility", T1)
    deployment = _identity("deployment-current-eligibility", T2)
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
            as_of=CURRENT_AT_START,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="current-eligibility-boundary",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="current-eligibility-loop",
            economic_goal_fingerprint=GOAL_SHA,
            risk_fingerprint=RISK_SHA,
            source_sha256="e" * 64,
            at=CURRENT_AT_START,
            training_identity=training,
            activation_binding=binding,
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_store,
        )
    return (
        path,
        training,
        deployment,
        policy,
        scope,
        binding,
        session,
        canonical_validate,
    )


def test_initialize_threads_current_as_of_to_deployment_eligibility(tmp_path) -> None:
    *_, canonical_validate = _initialize(tmp_path)

    assert (
        canonical_validate.call_args.kwargs["current_eligibility_as_of"]
        == CURRENT_AT_START
    )


def test_restart_revalidates_deployment_eligibility_at_restart_as_of(tmp_path) -> None:
    (
        path,
        training,
        deployment,
        policy,
        scope,
        _binding_value,
        session,
        _canonical_validate,
    ) = _initialize(tmp_path)
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
        ChampionAgentEpisode.resume(
            path,
            object(),
            object(),
            identity=deployment,
            checkpoint=checkpoint,
            as_of=CURRENT_AT_RESTART,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="current-eligibility-boundary",
            admissible_actions=frozenset({"WAIT"}),
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_store,
        )

    assert (
        canonical_validate.call_args.kwargs["current_eligibility_as_of"]
        == CURRENT_AT_RESTART
    )
