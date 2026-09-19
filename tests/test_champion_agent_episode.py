from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime, ExternalEffectState
from autosport.champion_agent_episode import (
    ChampionAgentEpisode,
    ChampionAgentEpisodeError,
)
from autosport.champion_policy import persist_policy_state
from autosport.learning_environment import (
    Action,
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore
from autosport.transparent_bandit_policy import BanditPolicyState


CONFIG_SHA256 = "c" * 64
GOAL_SHA256 = "a" * 64
RISK_SHA256 = "b" * 64
SOURCE_SHA256 = "e" * 64
PROTOCOL_ID = "protocol-champion-agent-v1"
STRATEGY_ID = "canonical-transparent-bandit"
MODEL_ID = "model-transparent-bandit-v2"
T0 = "2026-09-19T13:00:00Z"
T1 = "2026-09-19T13:05:00Z"
T2 = "2026-09-19T13:10:00Z"
T3 = "2026-09-19T13:11:00Z"
T4 = "2026-09-19T13:20:00Z"


def _learned_champion(identity):
    predecessor = BanditPolicyState.initial(
        environment_id=identity.environment_id,
        protocol_id=identity.protocol_id,
        config_sha256=CONFIG_SHA256,
        seed=identity.seed,
        action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    action = Action(
        environment_id=identity.environment_id,
        observation_id="1" * 64,
        action_type="WAIT",
        decided_at=T0,
    )
    reward = RewardEvidence(
        environment_id=identity.environment_id,
        action_id=action.action_id,
        outcome_id="2" * 64,
        reward=Decimal("1.25"),
        available_at=T1,
        truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=identity.environment_id,
        episode_id="3" * 64,
        step_index=1,
        observation_id=action.observation_id,
        action_id=action.action_id,
        outcome_id=reward.outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at=reward.available_at,
    )
    successor, _ = predecessor.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    return predecessor, successor


def _activation_authority(policy):
    strategy = SimpleNamespace(
        record_type="StrategyVersion",
        record_id=policy.policy_id,
        available_at=T1,
        record_sha256="8" * 64,
        payload={
            "strategy_version_id": policy.policy_id,
            "canonical_strategy_id": STRATEGY_ID,
            "environment_sha256": policy.environment_id,
            "config_sha256": policy.config_sha256,
            "model_version_id": MODEL_ID,
        }
    )
    model = SimpleNamespace(
        record_type="ModelVersion",
        record_id=MODEL_ID,
        available_at=T1,
        record_sha256="9" * 64,
        payload={
            "model_version_id": MODEL_ID,
            "environment_sha256": policy.environment_id,
            "config_sha256": policy.config_sha256,
            "research_protocol_id": policy.protocol_id,
            "seed": policy.seed,
        }
    )
    promotion = SimpleNamespace(
        record_type="PromotionDecision",
        record_id="promotion-champion-agent-v1",
        available_at=T2,
        record_sha256="6" * 64,
        payload={
            "action": "PROMOTE",
            "candidate_strategy_version_id": policy.policy_id,
            "candidate_model_version_id": MODEL_ID,
            "research_protocol_id": policy.protocol_id,
            "evaluation_bundle_id": "evaluation-champion-agent-v1",
            "evaluation_bundle_sha256": "f" * 64,
            "predecessor_strategy_version_id": None,
            "rollback_to_strategy_version_id": None,
        },
    )
    evaluation = SimpleNamespace(
        record_type="EvaluationBundle",
        record_id="evaluation-champion-agent-v1",
        record_sha256="7" * 64,
        available_at=T1,
        payload={
            "evaluation_bundle_id": "evaluation-champion-agent-v1",
            "bundle_sha256": "f" * 64,
            "evaluated_strategy_version_id": policy.policy_id,
            "evaluated_model_version_id": MODEL_ID,
        },
    )
    return strategy, model, promotion, evaluation


def _patched_authority(policy):
    strategy, model, promotion, evaluation = _activation_authority(policy)

    def get(_self, kind, record_id):
        records = {
            ("StrategyVersion", policy.policy_id): strategy,
            ("ModelVersion", MODEL_ID): model,
            ("EvaluationBundle", evaluation.record_id): evaluation,
        }
        return records.get((kind, record_id))

    def causal_records(_self, kind, *, as_of):
        records = {
            "PromotionDecision": promotion,
            "StrategyVersion": strategy,
            "ModelVersion": model,
            "EvaluationBundle": evaluation,
        }
        record = records.get(kind)
        return () if record is None else (record,)

    return (
        patch.object(
            ScientificRegistry,
            "champion_strategy",
            autospec=True,
            return_value=policy.policy_id,
        ),
        patch.object(ScientificRegistry, "get", autospec=True, side_effect=get),
        patch.object(
            ScientificRegistry,
            "causal_records",
            autospec=True,
            side_effect=causal_records,
        ),
    )


def _advance_to_decision(session, observation):
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


def test_promoted_policy_drives_fresh_agent_episode_and_survives_restart(tmp_path):
    identity = EnvironmentIdentity(
        source_id="lawful-provider:paper",
        config_id="champion-agent-config-v1",
        data_id="paper-evidence-v1",
        protocol_id=PROTOCOL_ID,
        cutoff_ts=T4,
        seed=17,
    )
    predecessor, champion = _learned_champion(identity)
    registry = ScientificRegistry.initialize_pristine(tmp_path / "registry.json")
    store = FactoryArtifactStore(tmp_path / "artifacts")
    persist_policy_state(store, champion)
    path = tmp_path / "agent-loop.json"

    patches = _patched_authority(champion)
    with patches[0], patches[1], patches[2]:
        session = ChampionAgentEpisode.initialize_pristine(
            path,
            registry,
            store,
            identity=identity,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA256,
            episode_key="next-paper-episode",
            admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
            loop_id="champion-agent-loop-v1",
            economic_goal_fingerprint=GOAL_SHA256,
            risk_fingerprint=RISK_SHA256,
            source_sha256=SOURCE_SHA256,
            at=T2,
        )

    assert predecessor.choose(
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"})
    ) == "PAPER_PROPOSAL"
    assert session.policy.policy_id == champion.policy_id
    assert session.environment.episode.policy_id == champion.policy_id
    assert session.agent_loop.snapshot().policy_id == champion.policy_id

    initial_checkpoint = session.environment.checkpoint()
    observation = Observation(
        environment_id=identity.environment_id,
        observed_at=T3,
        available_at=T3,
        evidence=(("market_state", "next-causal-paper-snapshot"),),
    )
    _advance_to_decision(session, observation)
    action = session.decide(observation, decision_at=T3)
    assert action.action_type == "WAIT"
    receipt = session.agent_loop.commit_action(
        action,
        episode=session.environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at=T3,
    )
    assert receipt.newly_committed is True

    patches = _patched_authority(champion)
    with patches[0], patches[1], patches[2]:
        reopened = ChampionAgentEpisode.resume(
            path,
            ScientificRegistry(tmp_path / "registry.json"),
            FactoryArtifactStore(tmp_path / "artifacts"),
            identity=identity,
            checkpoint=initial_checkpoint,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA256,
            episode_key="next-paper-episode",
            admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
        )
    assert reopened.agent_loop.snapshot().policy_id == champion.policy_id
    with pytest.raises(ChampionAgentEpisodeError, match="ACT_OR_ABSTAIN"):
        reopened.decide(observation, decision_at=T3)
    duplicate = reopened.agent_loop.commit_action(
        action,
        episode=reopened.environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at=T3,
    )
    assert duplicate.newly_committed is False
    assert duplicate.may_execute is False


def test_champion_episode_honors_external_wait_only_authority(tmp_path):
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
    patches = _patched_authority(champion)
    with patches[0], patches[1], patches[2]:
        session = ChampionAgentEpisode.initialize_pristine(
            tmp_path / "agent-loop.json",
            registry,
            store,
            identity=identity,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA256,
            episode_key="risk-narrowed-episode",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="risk-narrowed-agent-loop",
            economic_goal_fingerprint=GOAL_SHA256,
            risk_fingerprint=RISK_SHA256,
            source_sha256=SOURCE_SHA256,
            at=T2,
        )
    observation = Observation(
        identity.environment_id,
        T3,
        T3,
        (("market_state", "risk-narrowed-snapshot"),),
    )
    _advance_to_decision(session, observation)
    assert session.decide(observation, decision_at=T3).action_type == "WAIT"


def test_champion_evidence_cannot_arrive_after_agent_loop_start(tmp_path):
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

    with pytest.raises(ChampionAgentEpisodeError, match="not available"):
        ChampionAgentEpisode.initialize_pristine(
            tmp_path / "agent-loop.json",
            registry,
            store,
            identity=identity,
            as_of=T3,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA256,
            episode_key="future-champion-episode",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="future-champion-agent-loop",
            economic_goal_fingerprint=GOAL_SHA256,
            risk_fingerprint=RISK_SHA256,
            source_sha256=SOURCE_SHA256,
            at=T2,
        )

def test_resume_rejects_checkpoint_older_than_durable_agent_loop(tmp_path):
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

    environment = CausalLearningEnvironment(
        identity,
        episode_key="checkpoint-binding-episode",
        policy_id=champion.policy_id,
        admissible_actions=frozenset({"WAIT"}),
    )
    stale_checkpoint = environment.checkpoint()
    observation = Observation(
        identity.environment_id,
        T0,
        T0,
        (("market_state", "checkpoint-binding"),),
    )
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at=T0,
    )
    outcome = Outcome(
        identity.environment_id,
        action.action_id,
        T1,
        EvidenceTruth.OBSERVED,
        (("result", "paper-only"),),
    )
    reward = RewardEvidence(
        identity.environment_id,
        action.action_id,
        outcome.outcome_id,
        Decimal("0"),
        T1,
        EvidenceTruth.OBSERVED,
    )
    environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at=T1,
    )
    durable_checkpoint = environment.checkpoint()
    assert durable_checkpoint.checkpoint_id != stale_checkpoint.checkpoint_id

    path = tmp_path / "agent-loop.json"
    AgentLoopRuntime.initialize_pristine(
        path,
        loop_id="checkpoint-binding-loop",
        environment_checkpoint=durable_checkpoint,
        policy_id=champion.policy_id,
        economic_goal_fingerprint=GOAL_SHA256,
        risk_fingerprint=RISK_SHA256,
        source_sha256=SOURCE_SHA256,
        config_sha256=CONFIG_SHA256,
        at=T2,
    )

    patches = _patched_authority(champion)
    with patches[0], patches[1], patches[2]:
        with pytest.raises(
            ChampionAgentEpisodeError,
            match="checkpoint does not match durable AgentLoop checkpoint",
        ):
            ChampionAgentEpisode.resume(
                path,
                registry,
                store,
                identity=identity,
                checkpoint=stale_checkpoint,
                as_of=T2,
                canonical_strategy_id=STRATEGY_ID,
                config_sha256=CONFIG_SHA256,
                episode_key="checkpoint-binding-episode",
                admissible_actions=frozenset({"WAIT"}),
            )
