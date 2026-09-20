from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib

from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.policy_update_authority import (
    UTILITY_AUTHORITY_UNRESOLVED,
    attempt_utility_bound_update,
)
from autosport.policy_utility_evidence import (
    DecisionKind,
    PolicyUtilityEvidence,
    UtilityCompleteness,
    UtilityTruthClass,
)
from autosport.transparent_bandit_policy import BanditPolicyState


UTC_NOW = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


def _resolved_step():
    identity = EnvironmentIdentity(
        source_id="paper-replay-source-v1",
        config_id="learning-config-v1",
        data_id="dataset-sha256-example",
        protocol_id="rq-learning-utility-001",
        cutoff_ts="2026-09-20T14:00:00Z",
        seed=31,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="episode-utility-gate",
        policy_id="transparent-bandit-bootstrap-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-20T14:01:00Z",
        available_at="2026-09-20T14:01:01Z",
        evidence=(("quote", "2.10"),),
    )
    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at="2026-09-20T14:01:02Z",
    )
    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at="2026-09-20T14:10:00Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("result", "home-win"),),
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at="2026-09-20T14:10:01Z",
        truth=EvidenceTruth.SIMULATED,
        evidence=(("paper_execution", "modelled-fill"),),
        simulation_model_id="paper-fill-return-model-v1",
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at="2026-09-20T14:10:02Z",
    )
    config_sha256 = hashlib.sha256(b"utility-gate-config-v1").hexdigest()
    policy = BanditPolicyState.initial(
        environment_id=environment.environment_id,
        protocol_id="rq-learning-utility-001",
        config_sha256=config_sha256,
        seed=31,
        action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    return policy, action, reward, transition


def _utility(policy, action, reward, transition, **overrides):
    values = {
        "environment_id": policy.environment_id,
        "episode_id": transition.episode_id,
        "action_id": action.action_id,
        "outcome_id": reward.outcome_id,
        "reward_id": reward.reward_id,
        "transition_id": transition.transition_id,
        "policy_id": policy.policy_id,
        "model_id": "transparent-bandit",
        "strategy_id": "paper-proposal",
        "config_sha256": policy.config_sha256,
        "protocol_sha256": hashlib.sha256(b"rq-learning-utility-001").hexdigest(),
        "economic_goal_fingerprint": hashlib.sha256(b"goal").hexdigest(),
        "risk_fingerprint": hashlib.sha256(b"risk").hexdigest(),
        "bankroll_id": "paper-bankroll",
        "portfolio_identity": "paper-portfolio",
        "utility_definition_family": "owner-net-utility",
        "utility_definition_version": "v1",
        "utility_definition_sha256": hashlib.sha256(b"owner-net-utility-v1").hexdigest(),
        "completeness": UtilityCompleteness.INCOMPLETE,
        "truth_class": UtilityTruthClass.OBSERVED,
        "decision_kind": DecisionKind.POSITIONED,
        "available_at": UTC_NOW,
        "currency": "EUR",
        "utility_value": Decimal("0.25"),
    }
    values.update(overrides)
    return PolicyUtilityEvidence(**values)


def test_utility_gate_blocks_raw_reward_update_and_is_deterministic() -> None:
    policy, action, reward, transition = _resolved_step()
    utility = _utility(policy, action, reward, transition)

    successor, evidence = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )
    repeat_successor, repeat_evidence = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )

    assert successor == policy
    assert repeat_successor == policy
    assert successor.generation == 0
    assert evidence.reason_codes == (UTILITY_AUTHORITY_UNRESOLVED,)
    assert repeat_evidence == evidence
    assert repeat_evidence.evidence_id == evidence.evidence_id
    assert evidence.predecessor_policy_id == evidence.successor_policy_id == policy.policy_id
    assert evidence.utility_evidence_id == utility.evidence_id


def test_utility_gate_records_exact_binding_mismatch_without_mutation() -> None:
    policy, action, reward, transition = _resolved_step()
    utility = _utility(policy, action, reward, transition, reward_id="not-the-reward")

    successor, evidence = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )

    assert successor == policy
    assert successor.generation == 0
    assert evidence.reason_codes == (
        UTILITY_AUTHORITY_UNRESOLVED,
        "utility_reward_mismatch",
    )
